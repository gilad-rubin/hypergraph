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

    // Layout spacing (visible edge-to-edge gap)
    VERTICAL_GAP: 100,                    // Vertical gap between connected nodes

    // Wrapper offsets
    NODE_TYPE_OFFSETS: NODE_TYPE_OFFSETS, // Shadow/inner content offset by node type
    NODE_TYPE_TOP_INSETS: NODE_TYPE_TOP_INSETS, // Inset from wrapper top to visible top boundary
    DEFAULT_OFFSET: 10,                   // Fallback offset if type missing
    DEFAULT_TOP_INSET: 0,                 // Fallback top inset if type missing

    // Edge routing
    EDGE_CONVERGENCE_OFFSET: 20,          // Extra Y offset before merging into target stem
    FEEDBACK_EDGE_GUTTER: 70,             // Horizontal gutter for feedback edge routing
    FEEDBACK_EDGE_HEADROOM: 40,           // Extra headroom above feedback edge target
    FEEDBACK_EDGE_STEM: 32,               // Vertical stem size for feedback edges
    FEEDBACK_EDGE_STUB: 24,               // Stub length for feedback edge elbows
    EDGE_STRAIGHTEN_MAX_SHIFT: 180,       // Max X shift to keep a straight corridor path
    EDGE_MICRO_X_SNAP: 6,                 // Snap tiny X deviations to kill wiggles
    EDGE_ANGLE_WEIGHT: 1,                 // Penalize max angle in corridor routing
    EDGE_CURVE_WEIGHT: 6,                 // Penalize number of corridor X changes
    EDGE_TURN_WEIGHT: 1,                  // Penalize left/right sign flips
    EDGE_LATERAL_WEIGHT: 0.004,           // Penalize total sideways travel
    EDGE_NODE_PENALTY: 30,                // Penalize corridor segments that pass over nodes
    EDGE_NODE_CLEARANCE: 12,              // Extra buffer around nodes when checking overlap
    EDGE_NONSTRAIGHT_WEIGHT: 0.6,         // Penalize cumulative bend angles (many small bends)
    EDGE_EDGE_PENALTY: 20,                // Penalize corridor segments crossing other edges
    EDGE_EDGE_CLEARANCE: 10,              // Ignore edge crossings near shared endpoints
    EDGE_SHARED_TARGET_SPACING_SCALE: 1.15, // Scale separation for shared-target source spacing
    EDGE_SHARP_TURN_ANGLE: 35,            // Above this angle, force straight segments (if curveStyle < 1)
    EDGE_CURVE_STYLE: 0,                  // Rounded orthogonal routes (use elbow radius below)
    EDGE_ELBOW_RADIUS: 28,                // How big the rounded corner is (bigger = curve starts earlier)
    EDGE_MICRO_MERGE_ANGLE: 60,           // Max direction change (degrees) to drop short-segment waypoints
    EDGE_TURN_SOFTENING: 0.15,            // How much to soften corner angles (0=sharp 90°, higher=gentler)
    EDGE_SHOULDER_RATIO: 0.3,             // Where the horizontal run starts (0=at source, 1=at target)
    DATA_NODE_ALIGN_WEIGHT: 1,            // Pull DATA nodes toward their producer X (0..1)
    INPUT_NODE_ALIGN_WEIGHT: 0.9,         // Pull INPUT nodes toward downstream consumer X (0..1)
  };
});
