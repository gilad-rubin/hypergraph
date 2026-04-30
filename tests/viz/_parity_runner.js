// Node-side runner for the Python ↔ JS scene_builder parity harness.
//
// Reads JSON `{ir, opts}` from stdin, evaluates derivation.js +
// scene_builder.js, and writes the resulting scene to stdout. The
// harness in tests/viz/test_parity.py compares this against
// scene_builder.build_initial_scene's output for the same arguments.

const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2];
if (!ROOT) {
    console.error('usage: node _parity_runner.js <repo-root>');
    process.exit(1);
}

const ASSETS = path.join(ROOT, 'src', 'hypergraph', 'viz', 'assets');

// derivation.js attaches HypergraphDerivation; scene_builder.js attaches
// HypergraphSceneBuilder. Evaluate both in the current global scope.
eval(fs.readFileSync(path.join(ASSETS, 'derivation.js'), 'utf-8'));
eval(fs.readFileSync(path.join(ASSETS, 'scene_builder.js'), 'utf-8'));

const stdinBuf = fs.readFileSync(0, 'utf-8');
const {ir, opts} = JSON.parse(stdinBuf);

const scene = globalThis.HypergraphSceneBuilder.buildInitialScene(ir, opts);
process.stdout.write(JSON.stringify(scene));
