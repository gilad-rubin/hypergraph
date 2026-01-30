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
  const DEFAULT_OFFSET = VizConstants.DEFAULT_OFFSET || 10;
  const VERTICAL_GAP = VizConstants.VERTICAL_GAP || 60;

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
      [...separationConstraints, ...sharedTargetConstraints, ...parallelConstraints],
      layoutConfig,
      1
    );

    // Keep edge-to-edge spacing uniform by disabling density-based row expansion.
    expandDenseRows(edges, rows, coordSecondary, spaceY, orientation, 0);
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
        const separation =
          nodeA.width * 0.5 + spaceX * 0.5 + nodeB.width * 0.5;
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
  const EDGE_CONVERGENCE_OFFSET = 20;  // Y offset from target for convergence point
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

    // SINGLE-CORRIDOR ROUTING: Find ONE x-position that works for all intermediate rows
    // This eliminates squiggly edges by committing to a single corridor

    // Track used corridor positions to prevent edge overlap
    const usedCorridorPositions = { left: [], right: [] };
    const minEdgeSpacing = spaceX * 0.8;  // Minimum spacing between parallel edges

    // Sort edges by span length (ascending) - shorter edges processed first get inner positions
    // Longer edges get processed later and pushed to outer positions
    // Tiebreaker: target x position (left-to-right) for deterministic ordering
    const sortedEdges = [...edges].sort((a, b) => {
      const spanA = a.targetNode.row - a.sourceNode.row;
      const spanB = b.targetNode.row - b.sourceNode.row;
      if (spanA !== spanB) return spanA - spanB;
      // Tiebreaker: sort by target x position for consistent left-to-right ordering
      return a.targetNode.x - b.targetNode.x;
    });

    for (const edge of sortedEdges) {
      const source = edge.sourceNode;
      const target = edge.targetNode;

      edge.points = [];

      // The x position where the edge would naturally go
      // Use target position to guide routing - edges should trend toward their destination
      const naturalX = source.x + (target.x - source.x) * 0.5;

      // First pass: find which rows block the path from source to target
      // Check the full horizontal range the edge might traverse, not just the midpoint
      let firstBlockedRow = -1;
      let lastBlockedRow = -1;
      const blockedRows = [];

      for (let i = source.row + 1; i < target.row; i += 1) {
        let rowBlocks = false;
        for (const node of rows[i]) {
          // Check if this node blocks the natural path
          if (naturalX >= nodeLeft(node) - spaceX * 0.5 &&
              naturalX <= nodeRight(node) + spaceX * 0.5) {
            rowBlocks = true;
            break;
          }
        }

        if (rowBlocks) {
          if (firstBlockedRow === -1) firstBlockedRow = i;
          lastBlockedRow = i;
          blockedRows.push(i);
        }
      }

      // If no rows block, use the most direct path possible
      if (firstBlockedRow === -1) {
        const horizontalDist = Math.abs(target.x - source.x);
        const verticalDist = nodeTop(target) - nodeBottom(source);

        // Only add intermediate waypoint if horizontal distance is very large
        // This creates a gentler angle for edges that span significant horizontal distance
        const needsIntermediatePoint = horizontalDist > 100 && verticalDist > MIN_VERTICAL_DIST_FOR_WAYPOINT;
        
        if (needsIntermediatePoint) {
          // Add a single waypoint that moves most of the way toward target X
          // Use a higher ratio (0.7 instead of 0.5) to get closer to target faster
          const shoulderY = nodeBottom(source) + verticalDist * 0.3;
          const shoulderX = source.x + (target.x - source.x) * 0.7;
          edge.points.push({ x: shoulderX, y: shoulderY });
        }
        
        // Only add convergence point if target has multiple sources (needs merging)
        // or if horizontal distance is large (needs gradual approach)
        const needsConvergence = target.sources.length > 1 || horizontalDist > 80;
        if (needsConvergence && horizontalDist > 5) {
          const convergeY = calculateConvergenceY(target, stemMinTarget);
          edge.points.push({ x: target.x, y: convergeY });
        }
        continue;
      }

      // Calculate node bounds ONLY from the blocked rows
      // This ensures short edges don't get pushed far out due to nodes in unrelated rows
      let globalNodeLeft = Infinity;
      let globalNodeRight = -Infinity;

      for (const rowIdx of blockedRows) {
        for (const node of rows[rowIdx]) {
          globalNodeLeft = Math.min(globalNodeLeft, nodeLeft(node));
          globalNodeRight = Math.max(globalNodeRight, nodeRight(node));
        }
      }

      // Choose a single corridor x-position that clears the blocking nodes
      // Use left or right side, whichever is closer to the natural path
      const leftCorridorX = globalNodeLeft - spaceX;
      const rightCorridorX = globalNodeRight + spaceX;

      let corridorX;
      let corridorSide;
      if (Math.abs(naturalX - leftCorridorX) <= Math.abs(naturalX - rightCorridorX)) {
        corridorX = leftCorridorX;
        corridorSide = 'left';
      } else {
        corridorX = rightCorridorX;
        corridorSide = 'right';
      }

      // Calculate y positions for the routing points
      const firstBlockedNode = rows[firstBlockedRow][0];
      const lastBlockedNode = rows[lastBlockedRow][0];

      const y1 = nodeTop(firstBlockedNode) - spaceY;
      const y2 = nodeBottom(lastBlockedNode) + spaceY;

      // Check for conflicts with existing edges in this corridor
      // and offset if needed to prevent overlapping
      const usedPositions = usedCorridorPositions[corridorSide];
      for (const used of usedPositions) {
        // Check if y-ranges overlap
        const yOverlap = !(y2 < used.y1 || y1 > used.y2);
        if (yOverlap && Math.abs(corridorX - used.x) < minEdgeSpacing) {
          // Offset away from the nodes
          if (corridorSide === 'left') {
            corridorX = used.x - minEdgeSpacing;
          } else {
            corridorX = used.x + minEdgeSpacing;
          }
        }
      }

      // Record this corridor position
      usedPositions.push({ x: corridorX, y1, y2 });

      // Add routing points through the corridor
      edge.points.push({ x: corridorX, y: y1 });
      edge.points.push({ x: corridorX, y: y2 });
      
      // Add final convergence point at target X for smooth merging
      const convergeY = calculateConvergenceY(target, stemMinTarget);
      edge.points.push({ x: target.x, y: convergeY });
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

      const coordPrimary = orientation === 'vertical' ? 'y' : 'x';

      let pointMax = points[0][coordPrimary];

      for (const point of points) {
        if (point[coordPrimary] < pointMax) {
          point[coordPrimary] = pointMax;
        } else {
          pointMax = point[coordPrimary];
        }
      }

      edge.points = points;
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
      spreadX: 2.5,     // Slightly increased for better edge distribution
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
