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
    TYPE_HINT_MAX_CHARS: 27,              // Max chars for type hint line
    NODE_LABEL_MAX_CHARS: 27,             // Max chars for node label
    CHAR_WIDTH_PX: 7,                     // Approx char width for sizing estimates

    // Node sizing
    NODE_BASE_PADDING: 60,                // Default padding for nodes
    FUNCTION_NODE_BASE_PADDING: 54,       // Slightly tighter padding for FUNCTION nodes
    MAX_NODE_WIDTH: 340,                  // Clamp node width to avoid huge boxes

    // Nested graph layout
    GRAPH_PADDING: 26,                    // Padding inside graph containers
    HEADER_HEIGHT: 20,                    // Height reserved for graph header

    // Layout spacing (visible edge-to-edge gap)
    VERTICAL_GAP: 95,                     // Vertical gap between connected nodes
    BRANCH_CENTER_WEIGHT: 1,              // Pull BRANCH nodes to midpoint of targets (0=off, 1=fully centered)
    FAN_CENTER_WEIGHT: 0.8,               // Pull FUNCTION/GRAPH nodes to center over their targets (0=off, 1=fully centered)
    INPUT_FAN_CENTER_WEIGHT: 0.7,         // Pull INPUT nodes to center over their targets (0=off, 1=fully centered)

    // Wrapper offsets
    NODE_TYPE_OFFSETS: NODE_TYPE_OFFSETS, // Shadow/inner content offset by node type
    NODE_TYPE_TOP_INSETS: NODE_TYPE_TOP_INSETS, // Inset from wrapper top to visible top boundary
    DEFAULT_OFFSET: 10,                   // Fallback offset if type missing
    DEFAULT_TOP_INSET: 0,                 // Fallback top inset if type missing

    // Edge routing
    EDGE_CONVERGENCE_OFFSET: 15,          // Extra Y offset before merging into target stem
    EDGE_SOURCE_DIVERGE_OFFSET: 20,       // Shared vertical stem below source before edges split
    FEEDBACK_EDGE_GUTTER: 65,             // Horizontal gutter for feedback edge routing
    FEEDBACK_EDGE_HEADROOM: 100,          // Extra headroom above feedback edge target
    FEEDBACK_EDGE_STEM: 32,               // Vertical stem size for feedback edges
    FEEDBACK_EDGE_STUB: 24,               // Stub length for feedback edge elbows
    EDGE_STRAIGHTEN_MAX_SHIFT: 0,         // Max X shift to keep a straight corridor path
    EDGE_MICRO_X_SNAP: 20,               // Snap tiny X deviations to kill wiggles
    EDGE_ANGLE_WEIGHT: 0.1,               // Penalize max angle in corridor routing
    EDGE_CURVE_WEIGHT: 0.5,               // Penalize number of corridor X changes
    EDGE_NODE_PENALTY: 0,                 // Penalize corridor segments that pass over nodes
    EDGE_NODE_CLEARANCE: 0,               // Extra buffer around nodes when checking overlap
    EDGE_SHARED_TARGET_SPACING_SCALE: 0.5, // Scale separation for shared-target source spacing
    EDGE_SHARP_TURN_ANGLE: 0,             // Above this angle, force straight segments (if curveStyle < 1)
    EDGE_CURVE_STYLE: 0,                  // Rounded orthogonal routes (use elbow radius below)
    EDGE_ELBOW_RADIUS: 28,                // How big the rounded corner is (bigger = curve starts earlier)
    EDGE_TARGET_INSET: 12,                 // Horizontal padding from node edge for incoming arrows
    EDGE_MICRO_MERGE_ANGLE: 60,           // Max direction change (degrees) to drop short-segment waypoints
    EDGE_TURN_SOFTENING: 0,               // How much to soften corner angles (0=sharp 90°, higher=gentler)
    DATA_NODE_ALIGN_WEIGHT: 1,            // Pull DATA nodes toward their producer X (0..1)
    INPUT_NODE_ALIGN_WEIGHT: 0.9,         // Pull INPUT nodes toward downstream consumer X (0..1)
  };
});
