"""Bundled visualization assets.

- derivation.js / scene_builder.js: Shared graph-derivation and scene assembly
- viz_*.js / viz.js: Custom visualization runtime, components, and app bootstrap
- vendor/: Third-party libraries (React, ReactFlow, dagre, Tailwind)
"""

FIRST_PARTY_ASSET_NAMES = (
    "derivation.js",
    "scene_builder.js",
    "viz_runtime.js",
    "viz_layout.js",
    "viz_edges.js",
    "viz_nodes.js",
    "viz_controls.js",
    "viz_debug.js",
    "viz.js",
)

__all__ = ["FIRST_PARTY_ASSET_NAMES"]
