/**
 * Minimal Layout Engine for hypergraph visualization
 *
 * Drop-in replacement for constraint-layout.js with ~60% less code.
 * Vertical-only layout based on 4 rules:
 *   1. Centering — source nodes center over targets
 *   2. Convergence — edges merge into shared stems
 *   3. Crossing avoidance — soft penalties for node/edge crossings
 *   4. Straight lines — prefer vertical, penalize lateral movement
 *
 * Uses constraint solving (kiwi.js/Cassowary) for node positioning
 * and dynamic programming for edge routing through corridors.
 */
(function() {
  'use strict';

  const { Solver, Variable, Constraint, Operator, Strength } = window.kiwi;

  // ============================================================================
  // 1. CONSTANTS + UTILITIES
  // ============================================================================

  const VizConstants = window.HypergraphVizConstants || {};

  // Geometry helpers
  const clamp = (value, min, max) =>
    value < min ? min : value > max ? max : value;

  const snap = (value, unit) => Math.round(value / unit) * unit;

  const angle = (a, b) => Math.atan2(a.y - b.y, a.x - b.x);

  const nodeLeft = (node) => node.x - node.width * 0.5;
  const nodeRight = (node) => node.x + node.width * 0.5;
  const nodeTop = (node) => node.y - node.height * 0.5;
  const nodeBottom = (node) => node.y + node.height * 0.5;

  // Node type offsets: visible bounds vs wrapper bounds
  const NODE_TYPE_OFFSETS = VizConstants.NODE_TYPE_OFFSETS || {
    'PIPELINE': 26, 'GRAPH': 26, 'FUNCTION': 14, 'DATA': 6,
    'INPUT': 6, 'INPUT_GROUP': 6, 'BRANCH': 10,
  };
  const NODE_TYPE_TOP_INSETS = VizConstants.NODE_TYPE_TOP_INSETS || {
    'PIPELINE': 0, 'GRAPH': 0, 'FUNCTION': 0, 'DATA': 0,
    'INPUT': 0, 'INPUT_GROUP': 0, 'BRANCH': 3, 'END': 0,
  };
  const DEFAULT_OFFSET = VizConstants.DEFAULT_OFFSET ?? 10;
  const DEFAULT_TOP_INSET = VizConstants.DEFAULT_TOP_INSET ?? 0;

  // Layout constants
  const VERTICAL_GAP = VizConstants.VERTICAL_GAP ?? 60;
  const BRANCH_CENTER_WEIGHT = VizConstants.BRANCH_CENTER_WEIGHT ?? 1;
  const FAN_CENTER_WEIGHT = VizConstants.FAN_CENTER_WEIGHT ?? 0.8;
  const INPUT_FAN_CENTER_WEIGHT = VizConstants.INPUT_FAN_CENTER_WEIGHT ?? 0.7;
  const DATA_NODE_ALIGN_WEIGHT = VizConstants.DATA_NODE_ALIGN_WEIGHT ?? 1;
  const EDGE_SHARED_TARGET_SPACING_SCALE = VizConstants.EDGE_SHARED_TARGET_SPACING_SCALE ?? 0.5;

  // Edge routing constants
  const EDGE_CONVERGENCE_OFFSET = VizConstants.EDGE_CONVERGENCE_OFFSET ?? 15;
  const EDGE_SOURCE_DIVERGE_OFFSET = VizConstants.EDGE_SOURCE_DIVERGE_OFFSET ?? 20;
  const EDGE_STRAIGHTEN_MAX_SHIFT = VizConstants.EDGE_STRAIGHTEN_MAX_SHIFT ?? 0;
  const EDGE_MICRO_X_SNAP = VizConstants.EDGE_MICRO_X_SNAP ?? 20;
  const EDGE_ANGLE_WEIGHT = VizConstants.EDGE_ANGLE_WEIGHT ?? 0.1;
  const EDGE_CURVE_WEIGHT = VizConstants.EDGE_CURVE_WEIGHT ?? 0.5;
  const EDGE_NODE_PENALTY = VizConstants.EDGE_NODE_PENALTY ?? 0;
  const EDGE_NODE_CLEARANCE = VizConstants.EDGE_NODE_CLEARANCE ?? 0;

  // Resolve effective node type (collapsed PIPELINE → FUNCTION)
  const resolveNodeType = (node) => {
    let nodeType = node.data?.nodeType || 'FUNCTION';
    if (nodeType === 'PIPELINE' && !node.data?.isExpanded) nodeType = 'FUNCTION';
    return nodeType;
  };

  const nodeVisibleBottom = (node) => {
    const offset = NODE_TYPE_OFFSETS[resolveNodeType(node)] ?? DEFAULT_OFFSET;
    return nodeBottom(node) - offset;
  };

  const nodeVisibleTop = (node) => {
    const topInset = NODE_TYPE_TOP_INSETS[resolveNodeType(node)] ?? DEFAULT_TOP_INSET;
    return nodeTop(node) + topInset;
  };

  // Node type predicates
  const getNodeType = (node) => node.data?.nodeType || node.nodeType || 'FUNCTION';
  const isDataNode = (node) => getNodeType(node) === 'DATA';
  const isBranchNode = (node) => getNodeType(node) === 'BRANCH';
  const isFunctionLikeNode = (node) => {
    const t = getNodeType(node);
    return t === 'FUNCTION' || t === 'GRAPH' || (t === 'PIPELINE' && !node.data?.isExpanded);
  };
  const isInputNode = (node) => {
    const t = getNodeType(node);
    return t === 'INPUT' || t === 'INPUT_GROUP';
  };

  const groupByRow = (nodes, rowSnap = null) => {
    const rows = {};
    for (const node of nodes) {
      const key = rowSnap ? Math.round(node.y / rowSnap) * rowSnap : node.y;
      rows[key] = rows[key] || [];
      rows[key].push(node);
    }

    const rowNumbers = Object.keys(rows).map(parseFloat);
    rowNumbers.sort((a, b) => a - b);

    const sortedRows = rowNumbers.map((row) => rows[row]);
    for (let i = 0; i < sortedRows.length; i++) {
      sortedRows[i].sort((a, b) => compare(a.x, b.x, a.id, b.id));
      for (const node of sortedRows[i]) node.row = i;
    }

    return sortedRows;
  };

  const compare = (a, b, ...values) => {
    const delta = typeof a === 'string' ? a.localeCompare(b) : a - b;
    return delta !== 0 || values.length === 0 ? delta : compare(...values);
  };

  const offsetNode = (node, offset) => {
    node.x -= offset.x;
    node.y -= offset.y;
    node.order = node.x + node.y * 9999;
    return node;
  };

  const offsetEdge = (edge, offset) => {
    for (const point of edge.points) {
      point.x -= offset.x;
      point.y -= offset.y;
    }
    return edge;
  };

  // ============================================================================
  // 2. CONSTRAINT DEFINITIONS
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

  const parallelConstraint = {
    solve: (constraint) => {
      const { a, b, strength } = constraint;
      const resolve = strength * (a[constraint.property] - b[constraint.property]);
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
    solve: (constraint) => {
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
    solve: (constraint) => {
      const { edgeA, edgeB, separationA, separationB, strength } = constraint;
      const prop = constraint.property;

      const resolveSource =
        strength * ((edgeA.sourceNode[prop] - edgeB.sourceNode[prop] - separationA) / separationA);
      const resolveTarget =
        strength * ((edgeA.targetNode[prop] - edgeB.targetNode[prop] - separationB) / separationB);

      edgeA.sourceNode[prop] -= resolveSource;
      edgeB.sourceNode[prop] += resolveSource;
      edgeA.targetNode[prop] -= resolveTarget;
      edgeB.targetNode[prop] += resolveTarget;
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
  // 3. CONSTRAINT SOLVER
  // ============================================================================

  const solveLoose = (constraints, iterations) => {
    for (let i = 0; i < iterations; i++) {
      for (const constraint of constraints) {
        constraint.base.solve(constraint);
      }
    }
  };

  const solveStrict = (constraints) => {
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
      addVariable(constraint.a, constraint.property);
      addVariable(constraint.b, constraint.property);
    }

    let unsolvableCount = 0;
    for (const constraint of constraints) {
      try {
        solver.addConstraint(
          constraint.base.strict(
            constraint,
            null,
            variables[variableId(constraint.a, constraint.property)],
            variables[variableId(constraint.b, constraint.property)]
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

    for (const variable of Object.values(variables)) {
      variable.obj[variable.property] = variable.value();
    }
  };

  // ============================================================================
  // 4. LAYOUT ALGORITHM
  // ============================================================================

  /**
   * Initialize node X positions using barycenter heuristic.
   * Spreads leaf nodes evenly, then positions parent nodes
   * at the average X of their children.
   */
  const initializeBarycenterPositions = (nodes, edges) => {
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

    const leafNodes = nodes.filter(n => n._targets.length === 0);
    const nonLeafNodes = nodes.filter(n => n._targets.length > 0);
    const spacing = 200;

    leafNodes.forEach((node, i) => { node.x = i * spacing; });

    for (let pass = 0; pass < 5; pass++) {
      for (const node of nonLeafNodes) {
        if (node._targets.length > 0) {
          let sum = 0;
          for (const target of node._targets) sum += target.x;
          node.x = sum / node._targets.length;
        }
      }
    }

    for (const node of nodes) {
      delete node._targets;
      delete node._sources;
    }
  };

  /**
   * Reorder nodes within each row by barycenter of their connections.
   * 3-sweep algorithm: bottom-up (targets), top-down (both), bottom-up (both).
   */
  const reorderRowsByBarycenter = (rows, edges) => {
    if (rows.length === 0) return;

    const spacing = 200;
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

    const computeWeightedBarycenter = (node, sourceWeight, targetWeight) => {
      let sum = 0, count = 0;
      for (const src of node._sources) { sum += src.x * sourceWeight; count += sourceWeight; }
      for (const tgt of node._targets) { sum += tgt.x * targetWeight; count += targetWeight; }
      return count > 0 ? sum / count : null;
    };

    // Sweep 1: Bottom-to-top using targets only
    rows[rows.length - 1].forEach((node, i) => { node.x = i * spacing; });

    for (let rowIdx = rows.length - 2; rowIdx >= 0; rowIdx--) {
      const row = rows[rowIdx];
      for (const node of row) {
        node._barycenter = node._targets.length > 0
          ? node._targets.reduce((acc, t) => acc + t.x, 0) / node._targets.length
          : row.indexOf(node) * spacing;
      }
      row.sort((a, b) => compare(a._barycenter, b._barycenter, a.id, b.id));
      row.forEach((node, i) => { node.x = i * spacing; });
    }

    // Sweep 2: Top-to-bottom using weighted average
    for (let rowIdx = 1; rowIdx < rows.length; rowIdx++) {
      const row = rows[rowIdx];
      for (const node of row) {
        const weighted = computeWeightedBarycenter(node, 1, 1);
        node._barycenter = weighted !== null ? weighted : node.x;
      }
      row.sort((a, b) => compare(a._barycenter, b._barycenter, a.id, b.id));
      row.forEach((node, i) => { node.x = i * spacing; });
    }

    // Sweep 3: Bottom-to-top refinement
    for (let rowIdx = rows.length - 2; rowIdx >= 0; rowIdx--) {
      const row = rows[rowIdx];
      for (const node of row) {
        const weighted = computeWeightedBarycenter(node, 1, 1);
        node._barycenter = weighted !== null ? weighted : node.x;
      }
      row.sort((a, b) => compare(a._barycenter, b._barycenter, a.id, b.id));
      row.forEach((node, i) => { node.x = i * spacing; });
    }

    // Cleanup temporary fields
    for (const row of rows) {
      for (const node of row) {
        delete node._targets;
        delete node._sources;
        delete node._barycenter;
      }
    }
  };

  // --- Constraint creation helpers ---

  const createRowConstraints = (edges) =>
    edges.map((edge) => {
      const source = edge.sourceNode;
      const target = edge.targetNode;
      const sourceOffset = nodeBottom(source) - nodeVisibleBottom(source);
      const sourceVisibleHalf = Math.max(0, source.height * 0.5 - sourceOffset);
      const targetVisibleHalf = target.height * 0.5;
      return {
        base: rowConstraint,
        property: 'y',
        a: target,
        b: source,
        separation: VERTICAL_GAP + sourceVisibleHalf + targetVisibleHalf,
      };
    });

  const createCrossingConstraints = (edges, layoutConfig) => {
    const constraints = [];
    for (let i = 0; i < edges.length; i++) {
      const edgeA = edges[i];
      const { sourceNode: sourceA, targetNode: targetA } = edgeA;
      const degreeA = sourceA.sources.length + sourceA.targets.length +
        targetA.sources.length + targetA.targets.length;

      for (let j = i + 1; j < edges.length; j++) {
        const edgeB = edges[j];
        const { sourceNode: sourceB, targetNode: targetB } = edgeB;
        if (sourceA.row >= targetB.row || targetA.row <= sourceB.row) continue;

        const degreeB = sourceB.sources.length + sourceB.targets.length +
          targetB.sources.length + targetB.targets.length;

        constraints.push({
          base: crossingConstraint,
          property: 'x',
          edgeA, edgeB,
          separationA: sourceA.width * 0.5 + layoutConfig.spaceX + sourceB.width * 0.5,
          separationB: targetA.width * 0.5 + layoutConfig.spaceX + targetB.width * 0.5,
          strength: 1 / Math.max(1, (degreeA + degreeB) / 4),
        });
      }
    }
    return constraints;
  };

  const createParallelConstraints = (edges) =>
    edges.map(({ sourceNode, targetNode }) => ({
      base: parallelConstraint,
      property: 'x',
      a: sourceNode,
      b: targetNode,
      strength: 1.5 / Math.max(1, sourceNode.targets.length + targetNode.sources.length - 2),
    }));

  const buildDataSourceMap = (nodes, edges) => {
    const byId = new Map(nodes.map(n => [n.id, n]));
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

  const createDataAlignmentConstraints = (nodes, edges) => {
    if (!DATA_NODE_ALIGN_WEIGHT) return [];
    const byId = new Map(nodes.map(n => [n.id, n]));
    const dataSources = buildDataSourceMap(nodes, edges);
    const constraints = [];

    for (const node of nodes) {
      if (!isDataNode(node)) continue;
      const sourceId = node.data?.sourceId;
      const sourceNode = sourceId ? byId.get(sourceId) : dataSources.get(node.id);
      if (!sourceNode) continue;
      constraints.push({
        base: alignConstraint,
        property: 'x',
        a: node,
        b: sourceNode,
        strength: DATA_NODE_ALIGN_WEIGHT,
      });
    }
    return constraints;
  };

  const createSeparationConstraints = (rows, layoutConfig) => {
    const { spaceX, spreadX } = layoutConfig;
    const constraints = [];

    for (const rowNodes of rows) {
      rowNodes.sort((a, b) => compare(a.x, b.x, a.id, b.id));

      for (let j = 0; j < rowNodes.length - 1; j++) {
        const nodeA = rowNodes[j];
        const nodeB = rowNodes[j + 1];
        const degreeA = Math.max(1, nodeA.targets.length + nodeA.sources.length - 2);
        const degreeB = Math.max(1, nodeB.targets.length + nodeB.sources.length - 2);
        const spread = Math.min(10, degreeA * degreeB * spreadX);
        const space = snap(spread * spaceX, spaceX);

        constraints.push({
          base: separationConstraint,
          property: 'x',
          a: nodeA,
          b: nodeB,
          separation: nodeA.width * 0.5 + space + nodeB.width * 0.5,
        });
      }
    }
    return constraints;
  };

  const createSharedTargetConstraints = (edges, layoutConfig) => {
    const { spaceX } = layoutConfig;
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
      sources.sort((a, b) => compare(a.x, b.x, a.id, b.id));

      for (let i = 0; i < sources.length - 1; i++) {
        const nodeA = sources[i];
        const nodeB = sources[i + 1];
        constraints.push({
          base: separationConstraint,
          property: 'x',
          a: nodeA,
          b: nodeB,
          separation: (nodeA.width * 0.5 + spaceX * 0.5 + nodeB.width * 0.5) * EDGE_SHARED_TARGET_SPACING_SCALE,
        });
      }
    }
    return constraints;
  };

  // --- Unified centering pass ---

  /**
   * Center nodes over their targets in priority order: DATA → BRANCH → FUNCTION → INPUT.
   * All moves are clamped to respect row neighbors (no overlapping).
   */
  const centerNodesOverTargets = (rows, edges, layoutConfig) => {
    const nodeById = new Map();
    for (const row of rows) {
      for (const node of row) nodeById.set(node.id, node);
    }

    const targetsBySource = {};
    const dataSources = new Map();
    for (const edge of edges) {
      const srcId = edge.sourceNode.id;
      if (!targetsBySource[srcId]) targetsBySource[srcId] = [];
      if (!targetsBySource[srcId].includes(edge.targetNode)) {
        targetsBySource[srcId].push(edge.targetNode);
      }
      if (isDataNode(edge.targetNode)) {
        dataSources.set(edge.targetNode.id, edge.sourceNode);
      }
    }

    const computeSpace = (nodeA, nodeB) => {
      const degA = Math.max(1, nodeA.targets.length + nodeA.sources.length - 2);
      const degB = Math.max(1, nodeB.targets.length + nodeB.sources.length - 2);
      return snap(Math.min(10, degA * degB * layoutConfig.spreadX) * layoutConfig.spaceX, layoutConfig.spaceX);
    };

    const clampToRow = (row, idx, node, desiredX) => {
      let minX = -Infinity, maxX = Infinity;
      if (idx > 0) {
        const left = row[idx - 1];
        minX = left.x + left.width * 0.5 + computeSpace(left, node) + node.width * 0.5;
      }
      if (idx < row.length - 1) {
        const right = row[idx + 1];
        maxX = right.x - (node.width * 0.5 + computeSpace(node, right) + right.width * 0.5);
      }
      return minX <= maxX ? clamp(desiredX, minX, maxX) : null;
    };

    const targetMidpoint = (node, minTargets) => {
      const targets = targetsBySource[node.id];
      if (!targets || targets.length < minTargets) return null;
      return targets.reduce((s, t) => s + t.x, 0) / targets.length;
    };

    // Priority order: DATA → BRANCH → FUNCTION → INPUT
    const passes = [
      { test: isDataNode, getDesiredX: (node) => {
        const srcId = node.data?.sourceId;
        const src = srcId ? nodeById.get(srcId) : dataSources.get(node.id);
        return src ? src.x : null;
      }, weight: DATA_NODE_ALIGN_WEIGHT },
      { test: isBranchNode, getDesiredX: (node) => targetMidpoint(node, 2), weight: BRANCH_CENTER_WEIGHT },
      { test: isFunctionLikeNode, getDesiredX: (node) => targetMidpoint(node, 2), weight: FAN_CENTER_WEIGHT },
      { test: isInputNode, getDesiredX: (node) => targetMidpoint(node, 1), weight: INPUT_FAN_CENTER_WEIGHT },
    ];

    for (const { test, getDesiredX, weight } of passes) {
      if (!weight) continue;
      const w = Math.max(0, Math.min(1, weight));

      for (const row of rows) {
        row.sort((a, b) => compare(a.x, b.x, a.id, b.id));

        for (let i = 0; i < row.length; i++) {
          const node = row[i];
          if (!test(node)) continue;
          const desiredX = getDesiredX(node);
          if (desiredX === null) continue;
          const clamped = clampToRow(row, i, node, desiredX);
          if (clamped === null) continue;
          node.x += (clamped - node.x) * w;
        }
      }
    }
  };

  // --- Main layout function ---

  const layout = ({ nodes, edges, spaceX, spaceY, spreadX, layerSpaceY, iterations }) => {
    for (const node of nodes) {
      node.x = 0;
      node.y = 0;
    }

    const layoutConfig = {
      spaceX,
      spaceY,
      spreadX,
      layerSpace: (spaceY + layerSpaceY) * 0.5,
    };

    // Phase 1: Establish row positions (Y) via strict constraints
    const rowConstraints = createRowConstraints(edges);
    solveStrict(rowConstraints);

    // Phase 2: Initialize X positions using barycenter heuristic
    initializeBarycenterPositions(nodes, edges);

    const rows = groupByRow(nodes, spaceY);

    // Phase 3: Soft solve for crossing avoidance + parallel alignment
    const crossingConstraints = createCrossingConstraints(edges, layoutConfig);
    const parallelConstraints = createParallelConstraints(edges);
    const dataAlignmentConstraints = createDataAlignmentConstraints(nodes, edges);

    for (let i = 0; i < iterations; i++) {
      solveLoose(crossingConstraints, 1);
      solveLoose(parallelConstraints, 50);
    }

    // Phase 4: Reorder by barycenter, then strict solve for separation
    reorderRowsByBarycenter(rows, edges);

    const separationConstraints = createSeparationConstraints(rows, layoutConfig);
    const sharedTargetConstraints = createSharedTargetConstraints(edges, layoutConfig);

    solveStrict([
      ...separationConstraints,
      ...sharedTargetConstraints,
      ...parallelConstraints,
      ...dataAlignmentConstraints,
    ]);

    // Phase 5: Post-processing centering (DATA → BRANCH → FUNCTION → INPUT)
    centerNodesOverTargets(rows, edges, layoutConfig);
  };

  // ============================================================================
  // 5. EDGE ROUTING
  // ============================================================================

  const routing = ({
    nodes, edges, spaceX, spaceY, minPassageGap,
    stemUnit, stemMinSource, stemMinTarget, stemMax,
    stemSpaceSource, stemSpaceTarget,
  }) => {
    const rows = groupByRow(nodes, spaceY);

    // --- Corridor helpers ---

    const getRowCorridors = (row) => {
      if (!row || row.length === 0) return [];
      const firstNode = row[0];
      const rowExtended = [
        { ...firstNode, x: Number.MIN_SAFE_INTEGER },
        ...row,
        { ...firstNode, x: Number.MAX_SAFE_INTEGER },
      ];
      const corridors = [];
      for (let j = 0; j < rowExtended.length - 1; j++) {
        const node = rowExtended[j];
        const nextNode = rowExtended[j + 1];
        const nodeGap = nodeLeft(nextNode) - nodeRight(node);
        if (nodeGap < minPassageGap) continue;
        const offsetX = Math.min(spaceX, nodeGap * 0.5);
        const min = nodeRight(node) + offsetX;
        const max = nodeLeft(nextNode) - offsetX;
        if (min <= max) corridors.push({ min, max });
      }
      return corridors;
    };

    const intersectIntervals = (leftIntervals, rightIntervals) => {
      const result = [];
      for (const left of leftIntervals) {
        for (const right of rightIntervals) {
          const min = Math.max(left.min, right.min);
          const max = Math.min(left.max, right.max);
          if (min <= max) result.push({ min, max });
        }
      }
      return result;
    };

    const clampToIntervals = (intervals, x) => {
      let best = null, bestDist = Infinity;
      for (const interval of intervals) {
        const clamped = clamp(x, interval.min, interval.max);
        const dist = Math.abs(clamped - x);
        if (dist < bestDist) { bestDist = dist; best = clamped; }
      }
      return { x: best, dist: bestDist };
    };

    // --- Simplified corridor DP (no sign tracking, no edge-edge crossing) ---

    const buildCorridorPath = (rowIndices, preferredX, sourceY, targetX, sourceNode, targetNode) => {
      if (!rowIndices.length) return null;
      const corridorRows = rowIndices.map(idx => getRowCorridors(rows[idx]));
      if (corridorRows.some(row => row.length === 0)) return null;

      const uniqueNumbers = (values) => {
        const seen = new Map();
        for (const v of values) {
          if (!Number.isFinite(v)) continue;
          const key = Math.round(v * 2) / 2;
          if (!seen.has(key)) seen.set(key, key);
        }
        return Array.from(seen.values());
      };

      const buildRowCandidates = (intervals, nextIntervals, fallbackX, targetXVal) => {
        const candidates = [];
        for (const interval of intervals) {
          const ic = [];
          if (Number.isFinite(interval.min)) ic.push(interval.min);
          if (Number.isFinite(interval.max)) ic.push(interval.max);
          if (Number.isFinite(fallbackX)) ic.push(clamp(fallbackX, interval.min, interval.max));
          if (Number.isFinite(targetXVal)) ic.push(clamp(targetXVal, interval.min, interval.max));
          if (nextIntervals) {
            for (const ni of nextIntervals) {
              if (Number.isFinite(ni.min)) ic.push(clamp(ni.min, interval.min, interval.max));
              if (Number.isFinite(ni.max)) ic.push(clamp(ni.max, interval.min, interval.max));
            }
          }
          for (const v of uniqueNumbers(ic)) candidates.push(v);
        }
        return uniqueNumbers(candidates);
      };

      const expandCandidatesWithCarry = (candidates, intervals, prevLayer) => {
        if (!prevLayer || prevLayer.length === 0) return candidates;
        const expanded = [...candidates];
        const canUseX = (x) => intervals.some(iv => x >= iv.min && x <= iv.max);
        for (const state of prevLayer) {
          if (state && canUseX(state.x)) expanded.push(state.x);
        }
        return uniqueNumbers(expanded);
      };

      const rowTopYs = rowIndices.map(idx => nodeTop(rows[idx][0]) - spaceY);
      const rowBottomYs = rowIndices.map(idx => {
        const n = rows[idx][0];
        return nodeTop(n) - spaceY + n.height + spaceY;
      });

      // Node-hit detection for corridor segments
      const isExpandedContainer = (node) => {
        const nt = node.data?.nodeType;
        return (nt === 'PIPELINE' || nt === 'GRAPH') && node.data?.isExpanded;
      };

      const rectsByRow = new Map();
      const getRowRects = (rowIndex) => {
        if (rectsByRow.has(rowIndex)) return rectsByRow.get(rowIndex);
        const rects = [];
        for (const node of (rows[rowIndex] || [])) {
          if (node.id === sourceNode?.id || node.id === targetNode?.id) continue;
          if (isExpandedContainer(node)) continue;
          rects.push({
            left: nodeLeft(node) - EDGE_NODE_CLEARANCE,
            right: nodeRight(node) + EDGE_NODE_CLEARANCE,
            top: nodeTop(node) - EDGE_NODE_CLEARANCE,
            bottom: nodeBottom(node) + EDGE_NODE_CLEARANCE,
          });
        }
        rectsByRow.set(rowIndex, rects);
        return rects;
      };

      const segmentIntersectsRect = (ax, ay, bx, by, rect) => {
        if (ax === bx && ay === by) {
          return ax >= rect.left && ax <= rect.right && ay >= rect.top && ay <= rect.bottom;
        }
        let t0 = 0, t1 = 1;
        const dx = bx - ax, dy = by - ay;
        const p = [-dx, dx, -dy, dy];
        const q = [ax - rect.left, rect.right - ax, ay - rect.top, rect.bottom - ay];
        for (let i = 0; i < 4; i++) {
          if (p[i] === 0) { if (q[i] < 0) return false; continue; }
          const r = q[i] / p[i];
          if (p[i] < 0) { if (r > t1) return false; if (r > t0) t0 = r; }
          else { if (r < t0) return false; if (r < t1) t1 = r; }
        }
        return true;
      };

      const segmentNodeHits = (ax, ay, bx, by, rowA, rowB) => {
        if (EDGE_NODE_PENALTY <= 0) return 0;
        let hits = 0;
        for (const rect of getRowRects(rowA)) {
          if (segmentIntersectsRect(ax, ay, bx, by, rect)) hits++;
        }
        if (rowB !== rowA) {
          for (const rect of getRowRects(rowB)) {
            if (segmentIntersectsRect(ax, ay, bx, by, rect)) hits++;
          }
        }
        return hits;
      };

      const compareState = (a, b) => {
        if (!b) return -1;
        if (a.nodeHits !== b.nodeHits) return a.nodeHits - b.nodeHits;
        if (a.cost !== b.cost) return a.cost - b.cost;
        return 0;
      };

      // Build row candidates
      const rowCandidates = corridorRows.map((intervals, r) =>
        buildRowCandidates(intervals, corridorRows[r + 1] || [], preferredX, targetX)
      );
      if (rowCandidates.some(row => row.length === 0)) return null;

      // DP initial layer
      const initialLayer = rowCandidates[0].map(x => {
        const dx = x - preferredX;
        const dy = Math.max(1, rowTopYs[0] - sourceY);
        const ang = Math.atan2(Math.abs(dx), dy);
        const curves = Math.abs(dx) > 0.5 ? 1 : 0;
        const nodeHits = segmentNodeHits(preferredX, sourceY, x, rowTopYs[0], sourceNode?.row, rowIndices[0]);
        const cost = ang * EDGE_ANGLE_WEIGHT + curves * EDGE_CURVE_WEIGHT + nodeHits * EDGE_NODE_PENALTY;
        return { x, maxAngle: ang, curves, nodeHits, cost, prevIndex: null };
      });

      const allLayers = [initialLayer];

      // DP forward pass: 1 state per candidate (no sign tracking)
      for (let r = 1; r < rowCandidates.length; r++) {
        const prevLayer = allLayers[r - 1];
        const candidates = expandCandidatesWithCarry(rowCandidates[r], corridorRows[r], prevLayer);
        const nextLayer = new Array(candidates.length).fill(null);

        for (let j = 0; j < candidates.length; j++) {
          const x = candidates[j];
          for (let i = 0; i < prevLayer.length; i++) {
            const prev = prevLayer[i];
            if (!prev) continue;
            const dx = x - prev.x;
            const dy = Math.max(1, rowTopYs[r] - rowBottomYs[r - 1]);
            const ang = Math.atan2(Math.abs(dx), dy);
            const maxAngle = Math.max(prev.maxAngle, ang);
            const curves = prev.curves + (Math.abs(dx) > 0.5 ? 1 : 0);
            const nodeHits = prev.nodeHits +
              segmentNodeHits(prev.x, rowBottomYs[r - 1], x, rowTopYs[r], rowIndices[r - 1], rowIndices[r]);
            const cost = maxAngle * EDGE_ANGLE_WEIGHT + curves * EDGE_CURVE_WEIGHT + nodeHits * EDGE_NODE_PENALTY;
            const candidate = { x, maxAngle, curves, nodeHits, cost, prevIndex: i };
            if (compareState(candidate, nextLayer[j]) < 0) {
              nextLayer[j] = candidate;
            }
          }
        }
        allLayers.push(nextLayer);
      }

      // Find best path in final layer
      let best = null, bestIndex = 0;
      const lastLayer = allLayers[allLayers.length - 1];
      for (let i = 0; i < lastLayer.length; i++) {
        if (lastLayer[i] && (!best || compareState(lastLayer[i], best) < 0)) {
          best = lastLayer[i];
          bestIndex = i;
        }
      }
      if (!best) return null;

      // Trace back to build path
      const path = new Array(corridorRows.length);
      let idx = bestIndex;
      for (let r = allLayers.length - 1; r >= 0; r--) {
        const state = allLayers[r][idx];
        if (!state) break;
        path[r] = state.x;
        idx = state.prevIndex;
        if (idx === null || idx === undefined) break;
      }

      // Snap micro X moves to previous row's X when within threshold
      const snapped = [path[0]];
      for (let r = 1; r < path.length; r++) {
        const prevX = snapped[r - 1];
        if (Math.abs(path[r] - prevX) <= EDGE_MICRO_X_SNAP) {
          const canUsePrev = corridorRows[r].some(iv =>
            prevX >= iv.min - EDGE_MICRO_X_SNAP && prevX <= iv.max + EDGE_MICRO_X_SNAP
          );
          if (canUsePrev) { snapped.push(prevX); continue; }
        }
        snapped.push(path[r]);
      }
      return snapped;
    };

    // --- Sort edges by angle for corridor routing ---

    for (const node of nodes) {
      node.targets.sort((a, b) =>
        compare(
          angle(b.sourceNode, b.targetNode),
          angle(a.sourceNode, a.targetNode),
          a.targetNode.x,
          b.targetNode.x
        )
      );
    }

    // --- Corridor routing pass ---

    for (const edge of edges) {
      const source = edge.sourceNode;
      const target = edge.targetNode;
      edge.points = [];

      if (target.row - source.row > 1) {
        const sourceSeparation = Math.min(
          (source.width - stemSpaceSource) / source.targets.length,
          stemSpaceSource
        );
        const sourceEdgeDistance =
          source.targets.indexOf(edge) - (source.targets.length - 1) * 0.5;
        const preferredBaseX = source.x + sourceSeparation * sourceEdgeDistance;

        const rowIndices = [];
        for (let i = source.row + 1; i < target.row; i++) rowIndices.push(i);
        const rowIndexByRow = new Map(rowIndices.map((row, idx) => [row, idx]));

        // Try straight-through first
        let smoothXs = null;
        let allowed = [{ min: -Infinity, max: Infinity }];
        for (const idx of rowIndices) {
          const corridors = getRowCorridors(rows[idx]);
          if (!corridors.length) { allowed = []; break; }
          allowed = intersectIntervals(allowed, corridors);
          if (!allowed.length) break;
        }

        if (allowed.length) {
          const candidate = clampToIntervals(allowed, preferredBaseX);
          if (candidate.x !== null && candidate.dist <= EDGE_STRAIGHTEN_MAX_SHIFT) {
            smoothXs = rowIndices.map(() => candidate.x);
          }
        }

        // Fall back to DP corridor routing
        if (!smoothXs) {
          smoothXs = buildCorridorPath(rowIndices, preferredBaseX, source.y, target.x, source, target);
        }

        // Build waypoints from corridor path
        if (smoothXs) {
          for (let i = source.row + 1; i < target.row; i++) {
            const firstNode = rows[i][0];
            const rowY = nodeTop(firstNode) - spaceY;
            const offsetY = firstNode.height + spaceY;
            const rowIndex = rowIndexByRow.get(i);
            const baseX = (rowIndex !== null && rowIndex !== undefined)
              ? smoothXs[rowIndex]
              : source.x;
            edge.points.push({ x: baseX, y: rowY });
            edge.points.push({ x: baseX, y: rowY + offsetY });
          }
        }
      }
    }

    // --- Re-sort edges by waypoints for stem assembly ---

    for (const node of nodes) {
      node.targets.sort((a, b) =>
        compare(
          angle(b.sourceNode, b.points[0] || b.targetNode),
          angle(a.sourceNode, a.points[0] || a.targetNode),
          a.targetNode.x,
          b.targetNode.x
        )
      );
      node.sources.sort((a, b) =>
        compare(
          angle(a.points[a.points.length - 1] || a.sourceNode, a.targetNode),
          angle(b.points[b.points.length - 1] || b.sourceNode, b.targetNode),
          a.sourceNode.x,
          b.sourceNode.x
        )
      );
    }

    // --- Stem assembly + final cleanup ---

    const simplifyCollinear = (pts) => {
      if (pts.length < 3) return pts;
      const simplified = [pts[0]];
      for (let i = 1; i < pts.length - 1; i++) {
        const prev = simplified[simplified.length - 1];
        const curr = pts[i];
        const next = pts[i + 1];
        if ((prev.x === curr.x && curr.x === next.x) ||
            (prev.y === curr.y && curr.y === next.y)) continue;
        simplified.push(curr);
      }
      simplified.push(pts[pts.length - 1]);
      return simplified;
    };

    for (const edge of edges) {
      const source = edge.sourceNode;
      const target = edge.targetNode;

      const targetEdgeDistance =
        target.sources.indexOf(edge) - (target.sources.length - 1) * 0.5;
      const targetOffsetY =
        stemUnit * target.sources.length *
        (1 - Math.abs(targetEdgeDistance) / target.sources.length);

      // Source stem: shared diverge point for multi-target sources
      const sourceBottom = nodeVisibleBottom(source);
      const divergeLen = source.targets.length >= 2 ? EDGE_SOURCE_DIVERGE_OFFSET : 4;
      const sourceStem = [
        { x: source.x, y: sourceBottom },
        { x: source.x, y: sourceBottom + divergeLen },
      ];

      // Target stem: built-in convergence point
      const visTop = nodeVisibleTop(target);
      const convergeY = visTop - stemMinTarget - EDGE_CONVERGENCE_OFFSET;
      const targetStem = [
        { x: target.x, y: convergeY },
        { x: target.x, y: visTop - stemMinTarget },
        { x: target.x, y: visTop },
      ];

      // Assemble full path: source stem + corridor waypoints + target stem
      const points = [...sourceStem, ...edge.points, ...targetStem];

      // Snap micro X deviations between consecutive points
      for (let i = 1; i < points.length; i++) {
        if (Math.abs(points[i].x - points[i - 1].x) <= EDGE_MICRO_X_SNAP) {
          points[i].x = points[i - 1].x;
        }
      }

      // Remove collinear points
      const simplified = simplifyCollinear(points);

      // Enforce monotonic Y (no backwards segments)
      let pointMax = simplified[0].y;
      for (const point of simplified) {
        if (point.y < pointMax) point.y = pointMax;
        else pointMax = point.y;
      }

      edge.points = simplified;
    }
  };

  // ============================================================================
  // 6. ENTRY POINT
  // ============================================================================

  const defaultOptions = {
    layout: {
      spaceX: 42,
      spaceY: 140,
      layerSpaceY: 120,
      spreadX: 2.0,
      padding: 70,
      iterations: 30,
    },
    routing: {
      spaceX: 66,
      spaceY: 44,
      minPassageGap: 90,
      stemUnit: 5,
      stemMinSource: 0,
      stemMinTarget: 15,
      stemMax: 6,
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
      if (edge.sourceNode) edge.sourceNode.targets.push(edge);
      if (edge.targetNode) edge.targetNode.sources.push(edge);
    }
  };

  const bounds = (nodes, padding) => {
    const size = {
      min: { x: Infinity, y: Infinity },
      max: { x: -Infinity, y: -Infinity },
    };

    for (const node of nodes) {
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
   * @param {Object=} layers Unused (kept for API compatibility)
   * @param {String=} orientation Unused (always vertical)
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
      ...options.layout,
    });
    routing({ nodes, edges, ...options.routing });

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

  // Export to global scope (same API as constraint-layout.js)
  window.ConstraintLayout = {
    graph,
    defaultOptions,
  };

})();
