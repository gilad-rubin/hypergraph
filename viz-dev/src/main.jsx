/**
 * Bootstrap: expose window globals, load IIFE scripts, mount dev shell.
 *
 * The production viz uses IIFE modules that read from window globals
 * (React, ReactDOM, htm, etc). We import React/ReactDOM from npm and
 * set them on window BEFORE loading the IIFEs so everything shares
 * a single React instance.
 */
import React from 'react';
import * as ReactDOMClient from 'react-dom/client';
import * as ReactDOM from 'react-dom';
import DevApp from './DevApp';

// Expose React on window so IIFEs (and ReactFlow UMD) can find it.
// The production build uses react.production.min.js + react-dom.production.min.js
// which set window.React/ReactDOM. We replicate that from npm packages.
window.React = React;
window.ReactDOM = { ...ReactDOM, createRoot: ReactDOMClient.createRoot };

/**
 * Load a single <script> tag and return a promise that resolves on load.
 */
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    script.dataset.vizScript = src.split('/').pop().split('?')[0];
    script.onload = resolve;
    script.onerror = () => reject(new Error(`Failed to load: ${src}`));
    document.head.appendChild(script);
  });
}

/**
 * Load IIFE scripts sequentially (order matters — each depends on the previous).
 */
async function loadAllScripts() {
  const scripts = [
    '/assets/htm.min.js',
    '/assets/kiwi.bundled.js',
    '/assets/dagre.min.js',
    '/assets/constants.js',
    '/assets/reactflow.umd.js',
    '/assets/theme_utils.js',
    '/assets/layout-engine.js',
    '/assets/components.js',
    '/assets/app.js',
  ];

  for (const src of scripts) {
    await loadScript(src);
  }
}

/**
 * Reload only the scripts that capture constants at load time.
 * Cache-busts with ?t=timestamp so the browser re-evaluates them.
 */
export async function reloadDependentScripts() {
  const dependentScripts = [
    'layout-engine.js',
    'components.js',
    'app.js',
  ];

  // Remove existing dependent script tags
  for (const name of dependentScripts) {
    const existing = document.querySelector(`script[data-viz-script="${name}"]`);
    if (existing) existing.remove();
  }

  // Re-add sequentially with cache-bust
  const bust = Date.now();
  for (const name of dependentScripts) {
    await loadScript(`/assets/${name}?t=${bust}`);
  }
}

// Boot sequence
async function boot() {
  try {
    await loadAllScripts();

    // Verify required globals
    const required = [
      'React', 'ReactDOM', 'ReactFlow', 'htm', 'ConstraintLayout',
      'HypergraphVizTheme', 'HypergraphVizLayout',
      'HypergraphVizComponents', 'HypergraphVizApp', 'HypergraphVizConstants',
    ];
    const missing = required.filter(m => !window[m]);
    if (missing.length > 0) {
      console.error('Missing globals after script load:', missing);
      document.getElementById('fallback').textContent =
        'Missing modules: ' + missing.join(', ');
      return;
    }

    // Mount dev shell into #dev-root
    const devRoot = ReactDOMClient.createRoot(document.getElementById('dev-root'));
    devRoot.render(React.createElement(DevApp));

    console.log('[viz-dev] Boot complete. Globals loaded:', required.join(', '));
  } catch (err) {
    console.error('[viz-dev] Boot failed:', err);
    const fallback = document.getElementById('fallback');
    if (fallback) {
      fallback.textContent = 'Boot error: ' + err.message;
      fallback.style.color = '#f87171';
    }
  }
}

boot();
