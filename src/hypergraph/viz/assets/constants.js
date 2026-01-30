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
  };

  return {
    // Text layout
    TYPE_HINT_MAX_CHARS: 25,
    NODE_LABEL_MAX_CHARS: 25,
    CHAR_WIDTH_PX: 7,

    // Node sizing
    NODE_BASE_PADDING: 52,
    FUNCTION_NODE_BASE_PADDING: 48,
    MAX_NODE_WIDTH: 280,

    // Nested graph layout
    GRAPH_PADDING: 24,
    HEADER_HEIGHT: 32,

    // Layout spacing (visible edge-to-edge gap)
    VERTICAL_GAP: 100,

    // Complex graph layout scaling
    COMPLEX_GRAPH_NODE_THRESHOLD: 18,
    COMPLEX_GRAPH_SPACE_X_MULT: 1.6,

    // Wrapper offsets
    NODE_TYPE_OFFSETS: NODE_TYPE_OFFSETS,
    DEFAULT_OFFSET: 10,
  };
});
