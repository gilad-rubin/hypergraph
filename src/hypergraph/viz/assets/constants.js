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
    DEFAULT_OFFSET: 10,                   // Fallback offset if type missing

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
    EDGE_NODE_PENALTY: 10,                // Penalize corridor segments that pass over nodes
    EDGE_NODE_CLEARANCE: 6,               // Extra buffer around nodes when checking overlap
    EDGE_NONSTRAIGHT_WEIGHT: 0.6,         // Penalize cumulative bend angles (many small bends)
    EDGE_EDGE_PENALTY: 8,                 // Penalize corridor segments crossing other edges
    EDGE_EDGE_CLEARANCE: 6,               // Ignore edge crossings near shared endpoints
    EDGE_SHARED_TARGET_SPACING_SCALE: 1,  // Scale separation for shared-target source spacing
    EDGE_SHARP_TURN_ANGLE: 35,            // Above this angle, force straight segments (if curveStyle < 1)
    EDGE_CURVE_STYLE: 0.9,                // 0 = straight polyline, 1 = smooth curve, in-between = subtle
    EDGE_ELBOW_RADIUS: 20,                // Rounded corner radius for straight polylines
    DATA_NODE_ALIGN_WEIGHT: 1,            // Pull DATA nodes toward their producer X (0..1)
  };
});
