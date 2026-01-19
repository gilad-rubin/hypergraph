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

  const groupByRow = (nodes, orientation) => {
    const rows = {};
    const primaryCoord = orientation === 'vertical' ? 'y' : 'x';
    const secondaryCoord = orientation === 'vertical' ? 'x' : 'y';

    for (const node of nodes) {
      const key = node[primaryCoord];
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

    solveStrict([...rowConstraints, ...layerConstraints], layoutConfig, 1);

    const rows = groupByRow(nodes, orientation);

    const crossingConstraints = createCrossingConstraints(edges, layoutConfig);
    const parallelConstraints = createParallelConstraints(edges, layoutConfig);

    for (let i = 0; i < iterations; i += 1) {
      solveLoose(crossingConstraints, 1, layoutConfig);
      solveLoose(parallelConstraints, 50, layoutConfig);
    }

    const separationConstraints = createSeparationConstraints(rows, layoutConfig);

    solveStrict(
      [...separationConstraints, ...parallelConstraints],
      layoutConfig,
      1
    );

    expandDenseRows(edges, rows, coordSecondary, spaceY, orientation);
  };

  const createRowConstraints = (edges, layoutConfig) =>
    edges.map((edge) => ({
      base: rowConstraint,
      property: layoutConfig.coordSecondary,
      a: edge.targetNode,
      b: edge.sourceNode,
      separation: layoutConfig.spaceY,
    }));

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
      strength:
        0.6 /
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
    const rows = groupByRow(nodes, orientation);

    // Sort edges by angle for each node (standard algorithm)
    for (const node of nodes) {
      node.targets.sort((a, b) =>
        compare(
          angle(b.sourceNode, b.targetNode, orientation),
          angle(a.sourceNode, a.targetNode, orientation)
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
    const sortedEdges = [...edges].sort((a, b) => {
      const spanA = a.targetNode.row - a.sourceNode.row;
      const spanB = b.targetNode.row - b.sourceNode.row;
      return spanA - spanB;  // Shorter spans first (inner), longer spans later (outer)
    });

    for (const edge of sortedEdges) {
      const source = edge.sourceNode;
      const target = edge.targetNode;

      edge.points = [];

      // Find the ideal gap between edge source anchors
      const sourceSeparation = Math.min(
        (source.width - stemSpaceSource) / source.targets.length,
        stemSpaceSource
      );

      const sourceEdgeDistance =
        source.targets.indexOf(edge) - (source.targets.length - 1) * 0.5;

      const sourceOffsetX = sourceSeparation * sourceEdgeDistance;

      // The x position where the edge would naturally go (source.x + offset)
      const naturalX = source.x + sourceOffsetX;

      // First pass: find which rows block the natural path
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

      // If no rows block, no routing needed
      if (firstBlockedRow === -1) {
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

      // Add just TWO routing points - entry and exit of the single corridor
      edge.points.push({ x: corridorX, y: y1 });
      edge.points.push({ x: corridorX, y: y2 });
    }

    for (const node of nodes) {
      node.targets.sort((a, b) =>
        compare(
          angle(b.sourceNode, b.points[0] || b.targetNode, orientation),
          angle(a.sourceNode, a.points[0] || a.targetNode, orientation)
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
          )
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
        sourceStem = [
          {
            x: source.x,
            y: nodeBottom(source),
          },
          {
            x: source.x,
            y: nodeBottom(source) + stemMinSource,
          },
          {
            x: source.x,
            y:
              nodeBottom(source) +
              stemMinSource +
              Math.min(sourceOffsetY, stemMax),
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
      spaceX: 14,
      spaceY: 140,      // Increased from 110 for more vertical spacing
      layerSpaceY: 120, // Increased from 100 for more layer spacing
      spreadX: 2.2,
      padding: 100,
      iterations: 25,
    },
    routing: {
      spaceX: 26,
      spaceY: 36,       // Increased from 28 for better edge routing
      minPassageGap: 50, // Increased from 40 for wider passages
      stemUnit: 8,
      stemMinSource: 0,
      stemMinTarget: 15,
      stemMax: 10,
      stemSpaceSource: 6,
      stemSpaceTarget: 10,
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
      const x = node.x;
      const y = node.y;

      if (x < size.min.x) {
        size.min.x = x;
      }
      if (x > size.max.x) {
        size.max.x = x;
      }
      if (y < size.min.y) {
        size.min.y = y;
      }
      if (y > size.max.y) {
        size.max.y = y;
      }
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
