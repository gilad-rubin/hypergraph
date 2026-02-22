/**
 * Shared visualization constants.
 * Single source of truth for layout + rendering measurements.
 */
(function(root, factory) {
  var api = factory();
  if (root) root.HypergraphVizConstants = api;
})(typeof window !== 'undefined' ? window : this, function() {
  'use strict';

  var NODE_TYPE_OFFSETS = {
    'PIPELINE': 26,    // Expanded containers (p-6 padding + border)
    'GRAPH': 26,       // Collapsed containers (same styling)
    'FUNCTION': 14,    // Function nodes (shadow-lg)
    'DATA': 6,         // Data nodes (shadow-sm)
    'INPUT': 6,        // Input nodes (shadow-sm)
    'INPUT_GROUP': 6,  // Input group nodes (shadow-sm)
    'BRANCH': 10,      // Diamond nodes (drop-shadow filter)
    'END': 6,          // End node (shadow-sm, similar to DATA)
  };
  var NODE_TYPE_TOP_INSETS = {
    'PIPELINE': 0,
    'GRAPH': 0,
    'FUNCTION': 0,
    'DATA': 0,
    'INPUT': 0,
    'INPUT_GROUP': 0,
    // Rotated 95px diamond inside 140px wrapper leaves ~2.8px top inset.
    'BRANCH': 3,
    'END': 0,
  };

  return {
    // Text layout
    TYPE_HINT_MAX_CHARS: 25,              // Max chars for type hint line
    NODE_LABEL_MAX_CHARS: 25,             // Max chars for node label
    CHAR_WIDTH_PX: 7,                     // Approx char width for sizing estimates

    // Node sizing
    NODE_BASE_PADDING: 52,                // Default padding for nodes
    FUNCTION_NODE_BASE_PADDING: 48,       // Slightly tighter padding for FUNCTION nodes
    MAX_NODE_WIDTH: 280,                  // Clamp node width to avoid huge boxes

    // Nested graph layout
    GRAPH_PADDING: 24,                    // Padding inside graph containers
    HEADER_HEIGHT: 32,                    // Height reserved for graph header

    // Wrapper offsets
    NODE_TYPE_OFFSETS: NODE_TYPE_OFFSETS, // Shadow/inner content offset by node type
    NODE_TYPE_TOP_INSETS: NODE_TYPE_TOP_INSETS, // Inset from wrapper top to visible top boundary
    DEFAULT_OFFSET: 10,                   // Fallback offset if type missing
    DEFAULT_TOP_INSET: 0,                 // Fallback top inset if type missing

    // Edge routing (used by layout.js merge phases)
    EDGE_CONVERGENCE_OFFSET: 20,          // Extra Y offset before merging into target stem
    FEEDBACK_EDGE_GUTTER: 70,             // Horizontal gutter for feedback edge routing
    FEEDBACK_EDGE_HEADROOM: 40,           // Extra headroom above feedback edge target
    FEEDBACK_EDGE_STEM: 32,               // Vertical stem size for feedback edges
    FEEDBACK_EDGE_STUB: 24,               // Stub length for feedback edge elbows

    // Edge rendering (used by components.js)
    EDGE_SHARP_TURN_ANGLE: 35,            // Above this angle, force straight segments (if curveStyle < 1)
    EDGE_CURVE_STYLE: 0,                  // 0 = polyline, 1 = smooth curve, 0..1 = Catmull-Rom
    EDGE_ELBOW_RADIUS: 16,               // Rounded corner radius for orthogonal polylines
  };
});
