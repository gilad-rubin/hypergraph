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

    for (const node of nodes) {
      node.targets.sort((a, b) =>
        compare(
          angle(b.sourceNode, b.targetNode, orientation),
          angle(a.sourceNode, a.targetNode, orientation)
        )
      );
    }

    // Helper: check if x position collides with any node in intermediate rows
    const collidesWithNodes = (x, sourceRow, targetRow, margin = spaceX * 0.5) => {
      for (let i = sourceRow + 1; i < targetRow; i += 1) {
        for (const node of rows[i]) {
          if (x >= nodeLeft(node) - margin && x <= nodeRight(node) + margin) {
            return true;
          }
        }
      }
      return false;
    };

    // Helper: check if source and target are horizontally aligned
    const isAligned = (sourceX, targetX, tolerance = spaceX * 0.3) => {
      return Math.abs(sourceX - targetX) < tolerance;
    };

    // Helper: find safe corridors (x ranges) that avoid all intermediate nodes
    const findSafeCorridors = (sourceRow, targetRow) => {
      if (targetRow - sourceRow <= 1) {
        return [{ left: -Infinity, right: Infinity, center: 0 }];
      }

      // Find the leftmost and rightmost node bounds across all intermediate rows
      let globalLeft = Infinity;
      let globalRight = -Infinity;

      for (let i = sourceRow + 1; i < targetRow; i += 1) {
        for (const node of rows[i]) {
          globalLeft = Math.min(globalLeft, nodeLeft(node) - spaceX);
          globalRight = Math.max(globalRight, nodeRight(node) + spaceX);
        }
      }

      // If no nodes in intermediate rows
      if (globalLeft === Infinity) {
        return [{ left: -Infinity, right: Infinity, center: 0 }];
      }

      const corridors = [];

      // Left corridor (to the left of all nodes)
      corridors.push({
        left: -Infinity,
        right: globalLeft,
        center: globalLeft - spaceX * 1.5
      });

      // Right corridor (to the right of all nodes)
      corridors.push({
        left: globalRight,
        right: Infinity,
        center: globalRight + spaceX * 1.5
      });

      // Find gaps between nodes that span all intermediate rows
      for (let i = sourceRow + 1; i < targetRow; i += 1) {
        const rowNodes = rows[i];
        for (let j = 0; j < rowNodes.length - 1; j++) {
          const gapLeft = nodeRight(rowNodes[j]) + spaceX;
          const gapRight = nodeLeft(rowNodes[j + 1]) - spaceX;
          if (gapRight > gapLeft + minPassageGap) {
            const gapCenter = (gapLeft + gapRight) * 0.5;
            // Check if this gap is clear in ALL intermediate rows
            let clearInAllRows = true;
            for (let k = sourceRow + 1; k < targetRow; k += 1) {
              for (const node of rows[k]) {
                if (gapCenter >= nodeLeft(node) - spaceX * 0.5 &&
                    gapCenter <= nodeRight(node) + spaceX * 0.5) {
                  clearInAllRows = false;
                  break;
                }
              }
              if (!clearInAllRows) break;
            }
            if (clearInAllRows) {
              corridors.push({
                left: gapLeft,
                right: gapRight,
                center: gapCenter
              });
            }
          }
        }
      }

      return corridors;
    };

    // Track used x positions per y-range for edge spacing
    const usedRoutes = [];
    const minEdgeSpacing = spaceX * 0.8;  // Increased minimum spacing

    const findAvailableX = (preferredX, y1, y2, corridorLeft, corridorRight) => {
      // Check if preferredX conflicts with existing edges in the same y-range
      for (const route of usedRoutes) {
        // Check if y-ranges overlap
        const yOverlap = !(y2 < route.y1 || y1 > route.y2);
        if (yOverlap && Math.abs(preferredX - route.x) < minEdgeSpacing) {
          // Conflict - try to find alternative
          const tryLeft = route.x - minEdgeSpacing;
          const tryRight = route.x + minEdgeSpacing;

          // Prefer the option closer to preferredX
          if (Math.abs(tryLeft - preferredX) < Math.abs(tryRight - preferredX)) {
            if (corridorLeft === -Infinity || tryLeft >= corridorLeft + spaceX * 0.3) {
              preferredX = tryLeft;
            } else if (corridorRight === Infinity || tryRight <= corridorRight - spaceX * 0.3) {
              preferredX = tryRight;
            }
          } else {
            if (corridorRight === Infinity || tryRight <= corridorRight - spaceX * 0.3) {
              preferredX = tryRight;
            } else if (corridorLeft === -Infinity || tryLeft >= corridorLeft + spaceX * 0.3) {
              preferredX = tryLeft;
            }
          }
        }
      }
      return preferredX;
    };

    for (const edge of edges) {
      const source = edge.sourceNode;
      const target = edge.targetNode;

      edge.points = [];

      // Skip routing for adjacent rows - direct connection
      if (target.row - source.row <= 1) {
        continue;
      }

      const sourceX = source.x;
      const targetX = target.x;

      // RULE 1: If source and target are aligned, go straight (no intermediate points)
      if (isAligned(sourceX, targetX)) {
        // Check if the straight path is clear
        if (!collidesWithNodes(sourceX, source.row, target.row)) {
          continue;  // Straight line, no routing needed
        }
      }

      // RULE 2: Check if we can go straight at source.x or target.x
      const canGoStraightSource = !collidesWithNodes(sourceX, source.row, target.row);
      const canGoStraightTarget = !collidesWithNodes(targetX, source.row, target.row);

      const firstIntermediateRow = rows[source.row + 1];
      const lastIntermediateRow = rows[target.row - 1];

      if (!firstIntermediateRow || !lastIntermediateRow) {
        continue;
      }

      const y1 = nodeTop(firstIntermediateRow[0]) - spaceY * 0.5;
      const y2 = nodeBottom(lastIntermediateRow[0]) + spaceY * 0.5;

      // RULE 3: For orthogonal routing, we need explicit horizontal + vertical segments
      // The path is: source → (sourceX, y1) → (routeX, y1) → (routeX, y2) → (targetX, y2) → target

      if (canGoStraightSource && canGoStraightTarget) {
        // Can use simple L-shape or straight
        if (isAligned(sourceX, targetX, spaceX)) {
          continue;  // Nearly straight
        }
        // L-shape: go down from source, then horizontal to target
        // Points: (sourceX, midY), (targetX, midY)
        const midY = (y1 + y2) * 0.5;
        edge.points.push({ x: sourceX, y: midY });
        edge.points.push({ x: targetX, y: midY });
        usedRoutes.push({ x: (sourceX + targetX) * 0.5, y1: midY - 10, y2: midY + 10 });
        continue;
      }

      // Need corridor routing
      const corridors = findSafeCorridors(source.row, target.row);

      if (corridors.length === 0) {
        continue;
      }

      // Choose the best corridor (closest to midpoint)
      const midX = (sourceX + targetX) * 0.5;
      let bestCorridor = corridors[0];
      let bestDistance = Infinity;

      for (const corridor of corridors) {
        const dist = Math.abs(corridor.center - midX);
        if (dist < bestDistance) {
          bestDistance = dist;
          bestCorridor = corridor;
        }
      }

      // Find available x position (avoiding other edges)
      let routeX = findAvailableX(bestCorridor.center, y1, y2, bestCorridor.left, bestCorridor.right);

      // Final collision check
      if (collidesWithNodes(routeX, source.row, target.row, spaceX * 0.3)) {
        routeX = bestCorridor.center;
      }

      // ORTHOGONAL ROUTING: Add points for clean 90° bends
      // Path: source stem ends at (sourceX, stemEnd)
      //       → horizontal to (routeX, y1)
      //       → vertical to (routeX, y2)
      //       → horizontal to (targetX, y2)
      //       → target stem starts at (targetX, stemStart)

      // Add horizontal connector from source.x to routeX
      edge.points.push({ x: sourceX, y: y1 });      // End of vertical from source
      edge.points.push({ x: routeX, y: y1 });       // Horizontal move to corridor

      // Vertical segment through corridor (if y1 != y2)
      if (Math.abs(y2 - y1) > 10) {
        edge.points.push({ x: routeX, y: y2 });     // Vertical through corridor
      }

      // Horizontal connector from routeX to target.x
      edge.points.push({ x: targetX, y: y2 });      // Horizontal move to target

      // Track this route for spacing
      usedRoutes.push({ x: routeX, y1, y2 });
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

  const defaultOptions = {
    layout: {
      spaceX: 20,
      spaceY: 120,
      layerSpaceY: 110,
      spreadX: 2.5,
      padding: 100,
      iterations: 25,
    },
    routing: {
      spaceX: 40,
      spaceY: 35,
      minPassageGap: 60,
      stemUnit: 10,
      stemMinSource: 5,
      stemMinTarget: 20,
      stemMax: 15,
      stemSpaceSource: 12,
      stemSpaceTarget: 14,
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
